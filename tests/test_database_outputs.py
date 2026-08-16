"""SQLite and SQL script outputs (design document section 33).

These are the first writers that can express a relationship rather than merely
record one, so the tests are mostly about the constraint: does the foreign key
the schema declared become a foreign key the database enforces, and does the
column type describe the values the column actually holds.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from cacophony.core.errors import OutputError
from cacophony.generation.engine import GenerationEngine
from cacophony.outputs import (
    OUTPUT_FORMATS,
    SINGLE_FILE_FORMATS,
    SqliteWriter,
    SqlScriptWriter,
    align_to_records,
    create_writer,
    output_path_for,
)
from helpers import compile_from

ENTITIES: dict[str, Any] = {
    "team": {
        "count": 4,
        "primary_key": "team_id",
        "fields": {
            "team_id": {"type": "integer", "generator": "sequence"},
            "name": {"generator": "faker", "provider": "company"},
        },
    },
    "player": {
        "count": 20,
        "primary_key": "player_id",
        "fields": {
            "player_id": {"type": "integer", "generator": "sequence"},
            "team": {"generator": "reference", "entity": "team"},
            "shirt_number": {"type": "integer", "generator": "random", "min": 1, "max": 99},
            "active": {"generator": "boolean", "probability": 0.8},
            "joined": {"type": "date", "generator": "datetime"},
        },
    },
}


def write(entity_name: str, path: Path, fmt: str, count: int, **options: Any) -> Path:
    compiled = compile_from(ENTITIES)
    engine = GenerationEngine(compiled)
    entity = compiled.entity(entity_name)
    records = asyncio.run(engine.generate_batch(entity_name, count))

    writer = create_writer(
        fmt,
        path,
        columns=entity.spec.field_names(),
        entity=entity,
        entities=compiled.entities,
        **options,
    )

    async def run() -> None:
        await writer.open()
        await writer.write_batch(records)
        await writer.close()

    asyncio.run(run())
    return writer.path


def write_all(path: Path, fmt: str) -> Path:
    for name in ("team", "player"):
        write(name, path, fmt, ENTITIES[name]["count"])
    return path


class TestRegistration:
    def test_both_formats_are_available(self) -> None:
        assert OUTPUT_FORMATS["sqlite"] is SqliteWriter
        assert OUTPUT_FORMATS["sql"] is SqlScriptWriter

    def test_sqlite_is_one_file_for_the_whole_project(self, tmp_path: Path) -> None:
        assert "sqlite" in SINGLE_FILE_FORMATS
        first = output_path_for(tmp_path, "team", "sqlite", database_name="league")
        second = output_path_for(tmp_path, "player", "sqlite", database_name="league")
        assert first == second == tmp_path / "league.db"

    def test_sql_scripts_are_one_file_per_entity(self, tmp_path: Path) -> None:
        assert output_path_for(tmp_path, "team", "sql") == tmp_path / "team.sql"

    def test_an_unknown_format_lists_the_known_ones(self, tmp_path: Path) -> None:
        with pytest.raises(OutputError, match="sqlite"):
            create_writer("sqlyte", tmp_path / "x")

    def test_writers_that_do_not_want_an_entity_are_not_given_one(self, tmp_path: Path) -> None:
        """`create_writer` is called with the same arguments for every format."""
        compiled = compile_from(ENTITIES)
        writer = create_writer(
            "jsonl", tmp_path / "team.jsonl", entity=compiled.entity("team"), entities={}
        )
        assert writer.path == tmp_path / "team.jsonl"


class TestSqlite:
    def test_the_declared_foreign_key_is_enforced(self, tmp_path: Path) -> None:
        path = write_all(tmp_path / "league.db", "sqlite")
        connection = sqlite3.connect(path)
        try:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='player'"
            ).fetchone()[0]
            assert 'FOREIGN KEY ("team") REFERENCES "team" ("team_id")' in schema
        finally:
            connection.close()

    def test_a_broken_key_would_be_caught(self, tmp_path: Path) -> None:
        """The constraint is real, not decorative."""
        path = write_all(tmp_path / "league.db", "sqlite")
        connection = sqlite3.connect(path)
        try:
            connection.execute("INSERT INTO player VALUES (999, 4242, 7, 1, '2020-01-01')")
            connection.commit()
            assert connection.execute("PRAGMA foreign_key_check").fetchall() != []
        finally:
            connection.close()

    def test_every_entity_lands_in_one_database(self, tmp_path: Path) -> None:
        path = write_all(tmp_path / "league.db", "sqlite")
        connection = sqlite3.connect(path)
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert {"team", "player"} <= tables
            assert connection.execute("SELECT COUNT(*) FROM player").fetchone()[0] == 20
        finally:
            connection.close()

    def test_columns_take_the_type_of_the_values_they_hold(self, tmp_path: Path) -> None:
        path = write_all(tmp_path / "league.db", "sqlite")
        connection = sqlite3.connect(path)
        try:
            types = {row[1]: row[2] for row in connection.execute("PRAGMA table_info(player)")}
            assert types["player_id"] == "INTEGER"
            # A reference must match the key it points at, or the join fails.
            assert types["team"] == "INTEGER"
            assert types["shirt_number"] == "INTEGER"
            # SQLite has no boolean; INTEGER is where one goes.
            assert types["active"] == "INTEGER"
            stored = connection.execute("SELECT typeof(team) FROM player LIMIT 1").fetchone()[0]
            assert stored == "integer"
        finally:
            connection.close()

    def test_the_join_the_schema_promised_actually_works(self, tmp_path: Path) -> None:
        path = write_all(tmp_path / "league.db", "sqlite")
        connection = sqlite3.connect(path)
        try:
            joined = connection.execute(
                "SELECT COUNT(*) FROM player p JOIN team t ON p.team = t.team_id"
            ).fetchone()[0]
            assert joined == 20
        finally:
            connection.close()

    def test_a_field_named_after_a_keyword_is_still_a_column(self, tmp_path: Path) -> None:
        entities = {
            "thing": {
                "count": 2,
                "fields": {
                    "order": {"type": "integer", "generator": "sequence"},
                    "group": {"generator": "constant", "value": "x"},
                },
            }
        }
        compiled = compile_from(entities)
        engine = GenerationEngine(compiled)
        records = asyncio.run(engine.generate_batch("thing", 2))
        writer = create_writer(
            "sqlite", tmp_path / "k.db", entity=compiled.entity("thing"), entities=compiled.entities
        )

        async def run() -> None:
            await writer.open()
            await writer.write_batch(records)
            await writer.close()

        asyncio.run(run())
        connection = sqlite3.connect(tmp_path / "k.db")
        try:
            assert connection.execute('SELECT "order" FROM thing').fetchall() == [(1,), (2,)]
        finally:
            connection.close()


class TestSqliteResume:
    def test_align_trims_a_table_that_ran_ahead_of_its_checkpoint(self, tmp_path: Path) -> None:
        """A batch commits before its checkpoint is written; resuming from the
        checkpoint would then insert that batch twice."""
        path = write_all(tmp_path / "league.db", "sqlite")

        assert align_to_records(path, 12, "sqlite", table="player") == 12

        connection = sqlite3.connect(path)
        try:
            assert connection.execute("SELECT COUNT(*) FROM player").fetchone()[0] == 12
            # The rows kept are the first twelve, not an arbitrary twelve.
            kept = [row[0] for row in connection.execute("SELECT player_id FROM player")]
            assert kept == list(range(1, 13))
        finally:
            connection.close()

    def test_align_reports_a_short_table_rather_than_inventing_rows(self, tmp_path: Path) -> None:
        path = write_all(tmp_path / "league.db", "sqlite")
        assert align_to_records(path, 500, "sqlite", table="player") == 20

    def test_align_on_a_missing_file_is_zero(self, tmp_path: Path) -> None:
        assert align_to_records(tmp_path / "nothing.db", 10, "sqlite", table="player") == 0

    def test_align_on_a_missing_table_is_zero(self, tmp_path: Path) -> None:
        path = write_all(tmp_path / "league.db", "sqlite")
        assert align_to_records(path, 10, "sqlite", table="referee") == 0


class TestSqlScript:
    def test_the_script_loads_into_a_real_database(self, tmp_path: Path) -> None:
        write("team", tmp_path / "team.sql", "sql", 4)
        write("player", tmp_path / "player.sql", "sql", 20)

        script = (tmp_path / "team.sql").read_text() + (tmp_path / "player.sql").read_text()
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(script)
            assert connection.execute("SELECT COUNT(*) FROM player").fetchone()[0] == 20
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM player p JOIN team t ON p.team = t.team_id"
                ).fetchone()[0]
                == 20
            )
        finally:
            connection.close()

    def test_portable_types_rather_than_sqlite_ones(self, tmp_path: Path) -> None:
        write("player", tmp_path / "player.sql", "sql", 4)
        text = (tmp_path / "player.sql").read_text()
        assert '"active" BOOLEAN' in text
        assert '"joined" DATE' in text
        assert "TRUE" in text or "FALSE" in text

    def test_a_quote_in_a_value_survives(self, tmp_path: Path) -> None:
        entities = {
            "quip": {
                "count": 1,
                "fields": {"line": {"generator": "constant", "value": "it's a 'test'"}},
            }
        }
        compiled = compile_from(entities)
        records = asyncio.run(GenerationEngine(compiled).generate_batch("quip", 1))
        writer = create_writer(
            "sql", tmp_path / "q.sql", entity=compiled.entity("quip"), entities=compiled.entities
        )

        async def run() -> None:
            await writer.open()
            await writer.write_batch(records)
            await writer.close()

        asyncio.run(run())

        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript((tmp_path / "q.sql").read_text())
            assert connection.execute("SELECT line FROM quip").fetchone()[0] == "it's a 'test'"
        finally:
            connection.close()

    def test_a_continuation_part_does_not_recreate_the_table(self, tmp_path: Path) -> None:
        """Parts concatenate into one script; a second CREATE would drop part one."""
        write("player", tmp_path / "player.sql", "sql", 10)
        second = write("player", tmp_path / "player.sql", "sql", 10, part=1)

        assert second.name == "player.part0001.sql"
        text = second.read_text()
        assert "CREATE TABLE" not in text
        assert "INSERT INTO" in text

    def test_not_appendable_so_a_resume_starts_a_part(self) -> None:
        assert SqlScriptWriter.appendable is False
        assert SqliteWriter.appendable is True
