"""Output writers (design document sections 31, 33 and 34)."""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import pytest

from cacophony.core.errors import OutputError
from cacophony.core.provenance import ProvenanceMode
from cacophony.generation.engine import GenerationEngine
from cacophony.outputs import OUTPUT_FORMATS, create_writer, output_path_for
from helpers import compile_from

SCHEMA = {
    "employee": {
        "count": 40,
        "fields": {
            "employee_id": {"type": "string", "generator": "sequence", "format": "EMP-{0000}"},
            "name": {"type": "string", "generator": "faker", "provider": "name"},
            "age": {"type": "integer", "generator": "random", "min": 21, "max": 65},
            "hired_on": {
                "type": "date",
                "generator": "datetime",
                "start": "2020-01-01",
                "end": "2026-01-01",
            },
            "active": {"type": "boolean", "generator": "boolean", "probability": 0.9},
            "tags": {"type": "array", "generator": "constant", "value": ["a", "b"]},
        },
    }
}


@pytest.fixture
def records():
    compiled = compile_from(SCHEMA)
    return compiled, GenerationEngine(compiled).preview("employee", 40)


def write(fmt: str, path: Path, records: list, columns: list[str], **options) -> Path:
    async def run() -> None:
        writer = create_writer(fmt, path, columns=columns, **options)
        async with writer:
            for start in range(0, len(records), 7):  # several batches
                await writer.write_batch(records[start : start + 7])

    asyncio.run(run())
    return path


class TestJsonLines:
    def test_one_object_per_line(self, records, tmp_path) -> None:
        compiled, rows = records
        path = write("jsonl", tmp_path / "e.jsonl", rows, compiled.entity("employee").field_order)
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 40
        first = json.loads(lines[0])
        assert first["employee_id"] == "EMP-0001"
        assert isinstance(first["hired_on"], str)  # dates become ISO strings

    def test_ndjson_is_the_same_writer(self) -> None:
        assert OUTPUT_FORMATS["ndjson"] is OUTPUT_FORMATS["jsonl"]


class TestJson:
    def test_produces_a_valid_array(self, records, tmp_path) -> None:
        compiled, rows = records
        path = write("json", tmp_path / "e.json", rows, compiled.entity("employee").field_order)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, list) and len(data) == 40

    def test_empty_dataset_still_produces_valid_json(self, tmp_path) -> None:
        path = write("json", tmp_path / "empty.json", [], [])
        assert json.loads(path.read_text(encoding="utf-8")) == []


class TestCsv:
    def test_header_follows_the_declared_columns(self, records, tmp_path) -> None:
        compiled, rows = records
        columns = compiled.entity("employee").field_order
        path = write("csv", tmp_path / "e.csv", rows, columns)
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            assert reader.fieldnames == columns
            body = list(reader)
        assert len(body) == 40

    def test_nested_values_become_json_cells(self, records, tmp_path) -> None:
        compiled, rows = records
        path = write("csv", tmp_path / "e.csv", rows, compiled.entity("employee").field_order)
        with path.open(newline="", encoding="utf-8") as handle:
            first = next(iter(csv.DictReader(handle)))
        assert json.loads(first["tags"]) == ["a", "b"]


class TestParquet:
    def test_native_column_types_survive(self, records, tmp_path) -> None:
        pytest.importorskip("pyarrow")
        import pyarrow.parquet as pq

        compiled, rows = records
        path = write(
            "parquet", tmp_path / "e.parquet", rows, compiled.entity("employee").field_order
        )
        table = pq.read_table(path)
        assert table.num_rows == 40
        schema = {field.name: str(field.type) for field in table.schema}
        assert schema["age"] == "int64"
        assert schema["active"] == "bool"
        assert schema["hired_on"].startswith("date32")

    def test_batches_become_row_groups(self, records, tmp_path) -> None:
        """Section 31: bounded memory means one row group per batch."""
        pytest.importorskip("pyarrow")
        import pyarrow.parquet as pq

        compiled, rows = records
        path = write(
            "parquet", tmp_path / "e.parquet", rows, compiled.entity("employee").field_order
        )
        assert pq.ParquetFile(path).num_row_groups == 6  # 40 records in batches of 7


class TestRegistryAndPaths:
    def test_unknown_format_lists_alternatives(self, tmp_path) -> None:
        with pytest.raises(OutputError, match="Available formats"):
            create_writer("stone_tablet", tmp_path / "x")

    def test_path_helper_uses_the_conventional_extension(self, tmp_path) -> None:
        assert output_path_for(tmp_path, "employee", "parquet").name == "employee.parquet"
        assert output_path_for(tmp_path, "employee", "jsonl").name == "employee.jsonl"

    def test_parent_directories_are_created(self, records, tmp_path) -> None:
        compiled, rows = records
        path = write(
            "jsonl",
            tmp_path / "deep" / "nested" / "e.jsonl",
            rows,
            compiled.entity("employee").field_order,
        )
        assert path.exists()

    def test_writing_before_open_is_reported(self, tmp_path) -> None:
        writer = create_writer("jsonl", tmp_path / "e.jsonl")
        with pytest.raises(OutputError, match="not open"):
            asyncio.run(writer.write_batch([]))


class TestProvenanceInOutput:
    def test_provenance_is_absent_by_default(self, records, tmp_path) -> None:
        compiled, rows = records
        path = write("jsonl", tmp_path / "e.jsonl", rows, compiled.entity("employee").field_order)
        assert "_provenance" not in path.read_text(encoding="utf-8")

    def test_provenance_is_written_when_requested(self, tmp_path) -> None:
        compiled = compile_from(SCHEMA)
        engine = GenerationEngine(compiled, provenance=ProvenanceMode.FIELD)
        rows = engine.preview("employee", 5)
        path = write(
            "jsonl",
            tmp_path / "e.jsonl",
            rows,
            compiled.entity("employee").field_order,
            provenance=ProvenanceMode.FIELD,
        )
        first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert first["_provenance"]["fields"]["name"]["generator"] == "faker"


def test_round_trip_is_stable_across_runs(tmp_path) -> None:
    """Same seed, same schema, byte-identical output."""
    compiled = compile_from(SCHEMA)
    columns = compiled.entity("employee").field_order
    first = write(
        "jsonl", tmp_path / "a.jsonl", GenerationEngine(compiled).preview("employee", 20), columns
    )
    second = write(
        "jsonl", tmp_path / "b.jsonl", GenerationEngine(compiled).preview("employee", 20), columns
    )
    assert first.read_bytes() == second.read_bytes()
