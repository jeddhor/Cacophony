"""Database outputs (design document sections 33, 34, 42).

Section 33 lists SQLite and SQL INSERT scripts among the formats an MVP needs.
They are the first writers that can express what the relational phase produces:
a CSV of employees and a CSV of devices carry the foreign key as a string and
nothing more, while a SQLite file carries the *relationship*, and a foreign key
that does not resolve becomes an error rather than a curiosity.

Two decisions worth stating.

**One file, many tables.** Every entity in a run writes into the same SQLite
database, because a relational output split across three files is not a
relational output. The writer therefore knows its entity's name and shares the
connection.

**The schema is declared, not inferred.** Column types come from the project
schema rather than from the first batch of values, so a column that happens to
be null for the first thousand records still gets the type its field declared.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ..core.errors import OutputError
from ..core.record import to_jsonable
from ..core.types import DataType
from .writers import FileWriter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ..core.record import GeneratedRecord
    from ..schema.models import FieldSpec

__all__ = ["SqlScriptWriter", "SqliteWriter", "align_table", "sql_type_for"]

#: Cacophony's types, in SQLite's five storage classes.
_SQLITE_TYPES: dict[DataType, str] = {
    DataType.INTEGER: "INTEGER",
    DataType.BOOLEAN: "INTEGER",
    DataType.FLOAT: "REAL",
    DataType.DECIMAL: "NUMERIC",
    DataType.BINARY: "BLOB",
}

#: The same, in the types a portable SQL script would use.
_SQL_TYPES: dict[DataType, str] = {
    DataType.INTEGER: "INTEGER",
    DataType.BOOLEAN: "BOOLEAN",
    DataType.FLOAT: "DOUBLE PRECISION",
    DataType.DECIMAL: "DECIMAL",
    DataType.DATE: "DATE",
    DataType.TIME: "TIME",
    DataType.DATETIME: "TIMESTAMP",
    DataType.TEXT: "TEXT",
    DataType.JSON: "TEXT",
    DataType.OBJECT: "TEXT",
    DataType.ARRAY: "TEXT",
    DataType.BINARY: "BLOB",
}


def sql_type_for(spec: FieldSpec, *, dialect: str = "sqlite") -> str:
    """The column type for a field.

    The field's declared type is the whole answer. It is trustworthy because
    the compiler has already settled it: a field that named a generator but no
    type has taken the type that generator produces, and a reference has taken
    the type of the key it points at. A column type derived from anything else
    would risk describing values the column does not hold.
    """
    table = _SQLITE_TYPES if dialect == "sqlite" else _SQL_TYPES
    default = "TEXT"
    if dialect != "sqlite" and spec.type.is_textual:
        limit = spec.constraints.max_length
        return f"VARCHAR({limit})" if limit else "TEXT"
    return table.get(spec.type, default)


def _bind(value: Any) -> Any:
    """Convert a generated value into something a driver will accept."""
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (Decimal, UUID, Path)):
        return str(value)
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(to_jsonable(value), ensure_ascii=False)
    return str(value)


def _quote(name: str) -> str:
    """Quote an identifier, so a field called ``order`` is still a column."""
    return '"' + name.replace('"', '""') + '"'


class _SchemaAware:
    """Shared knowledge of what an entity's table looks like."""

    def _column_definitions(self, dialect: str) -> list[str]:
        entity = self.entity  # type: ignore[attr-defined]
        if entity is None:
            return [f"{_quote(column)} TEXT" for column in (self.columns or [])]  # type: ignore[attr-defined]

        definitions: list[str] = []
        primary = entity.spec.resolved_primary_key()
        for name in entity.spec.field_names():
            spec = entity.spec.fields[name]
            parts = [_quote(name), sql_type_for(spec, dialect=dialect)]
            if name == primary:
                parts.append("PRIMARY KEY")
            elif spec.unique:
                parts.append("UNIQUE")
            if not spec.nullable and spec.effective_null_probability <= 0 and name != primary:
                parts.append("NOT NULL")
            definitions.append(" ".join(parts))
        return definitions

    def _foreign_keys(self) -> list[str]:
        """``REFERENCES`` clauses for the entity's reference fields.

        This is the point of a database output: the relationship the schema
        declared becomes a constraint the database enforces, so a broken key
        is an error rather than a string nobody checked.
        """
        entity = self.entity  # type: ignore[attr-defined]
        if entity is None:
            return []

        clauses: list[str] = []
        for compiled in entity.fields:
            target = getattr(compiled.generator, "target", None)
            if not isinstance(target, str):
                continue
            target_field = getattr(compiled.generator, "target_field", None)
            if not target_field:
                target_entity = self.entities.get(target) if self.entities else None  # type: ignore[attr-defined]
                target_field = target_entity.spec.resolved_primary_key() if target_entity else None
            if not target_field:
                continue
            clauses.append(
                f"FOREIGN KEY ({_quote(compiled.name)}) "
                f"REFERENCES {_quote(target)} ({_quote(target_field)})"
            )
        return clauses


class SqliteWriter(FileWriter, _SchemaAware):
    """Write an entity into a table of a SQLite database (section 33).

    Every entity of a run shares one file, so the result is a database rather
    than a directory of unrelated tables. Rows go in inside a transaction per
    batch, which is what keeps a ten-million-row insert from taking a day.
    """

    format = "sqlite"
    extension = ".db"
    #: A table can be appended to, so an interrupted run resumes into it.
    appendable = True

    #: One connection per database path, shared by every entity's writer.
    _connections: dict[str, sqlite3.Connection] = {}
    _users: dict[str, int] = {}

    def __init__(
        self,
        path: str | Path,
        *,
        entity: Any = None,
        entities: dict[str, Any] | None = None,
        table: str | None = None,
        **options: Any,
    ) -> None:
        super().__init__(path, **options)
        self.entity = entity
        self.entities = entities or {}
        self.table = table or (entity.name if entity is not None else self.path.stem)
        self._connection: sqlite3.Connection | None = None
        self._insert = ""

    # -- lifecycle ---------------------------------------------------------- #

    async def open(self) -> None:
        await super().open()
        key = str(self.path.resolve())
        connection = self._connections.get(key)
        if connection is None:
            try:
                connection = sqlite3.connect(key)
            except sqlite3.Error as exc:
                raise OutputError(f"could not open {self.path}: {exc}") from exc
            # Bulk loading, not durability: this file is regenerable by
            # definition, so paying for fsync on every batch buys nothing.
            connection.execute("PRAGMA journal_mode=MEMORY")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
            self._connections[key] = connection
            self._users[key] = 0
        self._users[key] += 1
        self._connection = connection

        self._create_table()

    def _create_table(self) -> None:
        assert self._connection is not None
        columns = self._column_definitions("sqlite")
        clauses = [*columns, *self._foreign_keys()]
        statement = (
            f"CREATE TABLE IF NOT EXISTS {_quote(self.table)} (\n  " + ",\n  ".join(clauses) + "\n)"
        )
        try:
            if not self.append:
                self._connection.execute(f"DROP TABLE IF EXISTS {_quote(self.table)}")
            self._connection.execute(statement)
            self._connection.commit()
        except sqlite3.Error as exc:
            raise OutputError(f"could not create table {self.table}: {exc}") from exc

        names = self._column_names()
        placeholders = ", ".join("?" for _ in names)
        self._insert = (
            f"INSERT INTO {_quote(self.table)} "
            f"({', '.join(_quote(name) for name in names)}) VALUES ({placeholders})"
        )

    def _column_names(self) -> list[str]:
        if self.entity is not None:
            return list(self.entity.spec.field_names())
        return list(self.columns or [])

    async def write_batch(self, records: Sequence[GeneratedRecord]) -> None:
        if not records or self._connection is None:
            return
        names = self._column_names()
        rows = [tuple(_bind(record.values.get(name)) for name in names) for record in records]
        try:
            self._connection.executemany(self._insert, rows)
            self._connection.commit()
        except sqlite3.Error as exc:
            raise OutputError(f"could not insert into {self.table}: {exc}") from exc
        self.records_written += len(records)

    async def close(self) -> None:
        key = str(self.path.resolve())
        if self._connection is not None:
            self._connection.commit()
            self._users[key] = self._users.get(key, 1) - 1
            # The last entity out closes the database; the others are still
            # writing into it.
            if self._users[key] <= 0:
                self._connection.close()
                self._connections.pop(key, None)
                self._users.pop(key, None)
            self._connection = None
        await super().close()

    def describe(self) -> str:
        return f"sqlite:{self.path}#{self.table}"


def align_table(path: str | Path, records: int, table: str) -> int:
    """Make a table hold exactly ``records`` rows, and say how many it holds.

    The SQLite equivalent of trimming a JSON Lines file before appending to it.
    Rows commit per batch and the checkpoint is written after the commit, so
    the table can legitimately be a batch ahead of what the store believes;
    resuming from the store's number would then insert a batch twice. Deleting
    the surplus is what keeps a resumed database free of duplicates.
    """
    file = Path(path)
    if not file.exists():
        return 0

    key = str(file.resolve())
    shared = SqliteWriter._connections.get(key)
    connection = shared or sqlite3.connect(key)
    try:
        cursor = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
        if cursor.fetchone() is None:
            return 0
        count = int(connection.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])
        if count > records:
            connection.execute(
                f"DELETE FROM {_quote(table)} WHERE rowid IN "
                f"(SELECT rowid FROM {_quote(table)} ORDER BY rowid LIMIT -1 OFFSET ?)",
                (records,),
            )
            connection.commit()
    except sqlite3.Error as exc:
        raise OutputError(f"could not reconcile table {table} in {file}: {exc}") from exc
    finally:
        if shared is None:
            connection.close()
    return min(count, records)


class SqlScriptWriter(FileWriter, _SchemaAware):
    """Write an entity as a SQL script (design document section 33).

    Portable rather than clever: ``CREATE TABLE`` followed by multi-row
    ``INSERT`` statements, in ANSI SQL that PostgreSQL, MySQL and SQL Server
    will all accept. It is the format you hand to a database Cacophony has no
    adapter for.

    Not appendable, despite being a text file. Counting how many rows a
    half-written script already contains means parsing SQL, and a resume that
    guesses wrong either duplicates rows or drops them. A resumed run writes a
    new part instead, and parts concatenate in order into one valid script -
    the second part omits the ``CREATE TABLE`` that the first one already has.
    """

    format = "sql"
    extension = ".sql"
    appendable = False

    def __init__(
        self,
        path: str | Path,
        *,
        entity: Any = None,
        entities: dict[str, Any] | None = None,
        table: str | None = None,
        dialect: str = "ansi",
        rows_per_statement: int = 500,
        **options: Any,
    ) -> None:
        super().__init__(path, **options)
        self.entity = entity
        self.entities = entities or {}
        self.table = table or (entity.name if entity is not None else self.path.stem)
        self.dialect = dialect
        self.rows_per_statement = max(1, rows_per_statement)

    async def open(self) -> None:
        await super().open()
        try:
            self._handle = self.path.open("a" if self.append else "w", encoding="utf-8")
        except OSError as exc:
            raise OutputError(f"could not open {self.path} for writing: {exc}") from exc

        if self.append or self.part:
            # A continuation part: the table already exists in part one, and
            # re-issuing the DDL here would drop the rows it holds.
            return

        columns = self._column_definitions(self.dialect)
        clauses = [*columns, *self._foreign_keys()]
        self._handle.write(
            f"-- Generated by Cacophony\nDROP TABLE IF EXISTS {_quote(self.table)};\n"
            f"CREATE TABLE {_quote(self.table)} (\n  " + ",\n  ".join(clauses) + "\n);\n\n"
        )

    async def write_batch(self, records: Sequence[GeneratedRecord]) -> None:
        if not records or self._handle is None:
            return
        names = self._column_names()
        header = (
            f"INSERT INTO {_quote(self.table)} "
            f"({', '.join(_quote(name) for name in names)}) VALUES\n"
        )

        chunks: list[str] = []
        for start in range(0, len(records), self.rows_per_statement):
            chunk = records[start : start + self.rows_per_statement]
            values = ",\n".join(
                "  (" + ", ".join(_literal(record.values.get(name)) for name in names) + ")"
                for record in chunk
            )
            chunks.append(header + values + ";\n")
        self._handle.write("".join(chunks))
        # Flushed per batch so the file on disk matches the checkpoint that is
        # about to be written for it.
        self._handle.flush()
        self.records_written += len(records)

    def _column_names(self) -> list[str]:
        if self.entity is not None:
            return list(self.entity.spec.field_names())
        return list(self.columns or [])

    def describe(self) -> str:
        return f"sql:{self.path}#{self.table}"


def _literal(value: Any) -> str:
    """Render a value as a SQL literal.

    Strings are single-quoted with doubled quotes inside - the escaping every
    dialect agrees on. Nothing here interpolates user input into a query; this
    writes a file for a human or a loader to run, and a value containing a
    quote must survive that intact.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    bound = _bind(value)
    text = str(bound)
    return "'" + text.replace("'", "''") + "'"
