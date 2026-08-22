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


class TestPartitionedOutput:
    """Section 34's ``partition_by``: one dataset, a tree of directories."""

    @staticmethod
    def _write(fmt, path, records, columns, **options):
        return write(fmt, path, records, columns, partition_by=["active"], **options)

    def test_a_directory_per_distinct_value(self, records, tmp_path) -> None:
        compiled, rows = records
        root = tmp_path / "employee"
        self._write("jsonl", root, rows, compiled.entity("employee").field_order)

        partitions = sorted(child.name for child in root.iterdir())
        assert partitions == ["active=False", "active=True"]
        written = sum(
            len(part.read_text(encoding="utf-8").strip().splitlines())
            for part in root.rglob("*.jsonl")
        )
        assert written == 40

    def test_text_formats_keep_the_partition_column(self, records, tmp_path) -> None:
        """Nothing puts it back for JSON Lines, so removing it would lose data."""
        compiled, rows = records
        root = tmp_path / "employee"
        self._write("jsonl", root, rows, compiled.entity("employee").field_order)

        line = next(iter(root.rglob("*.jsonl"))).read_text(encoding="utf-8").splitlines()[0]
        assert "active" in json.loads(line)

    def test_parquet_drops_it_so_the_dataset_reads_back(self, records, tmp_path) -> None:
        """The reader reconstructs it from the path, and refuses both at once.

        Leaving the column in the files as well produced "Field active has
        incompatible types: string vs dictionary" from PyArrow's dataset reader,
        which is the whole partitioned directory being unreadable.
        """
        pq = pytest.importorskip("pyarrow.parquet")
        compiled, rows = records
        root = tmp_path / "employee"
        self._write("parquet", root, rows, compiled.entity("employee").field_order)

        table = pq.read_table(str(root))
        assert table.num_rows == 40
        assert "active" in table.column_names

    def test_the_convention_can_be_overridden(self, records, tmp_path) -> None:
        pytest.importorskip("pyarrow.parquet")
        compiled, rows = records
        root = tmp_path / "employee"
        self._write(
            "parquet",
            root,
            rows,
            compiled.entity("employee").field_order,
            drop_partition_columns=False,
        )
        import pyarrow.parquet as pq

        one = next(iter(root.rglob("*.parquet")))
        assert "active" in pq.ParquetFile(str(one)).schema.names

    def test_nulls_get_a_name_a_path_can_carry(self, tmp_path) -> None:
        compiled = compile_from(
            {
                "e": {
                    "count": 4,
                    "fields": {
                        "n": {"type": "integer", "generator": "sequence"},
                        "maybe": {"generator": "null", "nullable": True},
                    },
                }
            }
        )
        rows = GenerationEngine(compiled, validate=False).preview("e", 4)
        root = tmp_path / "e"
        write("jsonl", root, rows, ["n", "maybe"], partition_by=["maybe"])
        assert [child.name for child in root.iterdir()] == ["maybe=__null__"]

    def test_too_many_partitions_is_refused_with_advice(self, records, tmp_path) -> None:
        """Partitioning on a high-cardinality column makes a million tiny files."""
        compiled, rows = records
        with pytest.raises(OutputError, match="more than 4 partitions"):
            write(
                "jsonl",
                tmp_path / "employee",
                rows,
                compiled.entity("employee").field_order,
                partition_by=["employee_id"],
                max_partitions=4,
            )

    def test_a_single_file_format_cannot_be_partitioned(self, records, tmp_path) -> None:
        compiled, rows = records
        with pytest.raises(OutputError, match="nothing to partition"):
            write(
                "sqlite",
                tmp_path / "e.db",
                rows,
                compiled.entity("employee").field_order,
                partition_by=["active"],
            )

    def test_counting_a_partitioned_directory_sums_its_files(self, records, tmp_path) -> None:
        """What resume asks: how much of this is already on disk?"""
        from cacophony.outputs import align_to_records

        compiled, rows = records
        root = tmp_path / "employee"
        self._write("jsonl", root, rows, compiled.entity("employee").field_order)
        assert align_to_records(root, 0, "jsonl") == 40


# --------------------------------------------------------------------------- #
# Section 33's destinations that are not files
# --------------------------------------------------------------------------- #


class TestElasticsearch:
    """The bulk API, against a transport that answers instead of a cluster."""

    def _writer(self, handler, **options):
        import httpx

        from cacophony.outputs import create_writer as build

        writer = build("elasticsearch", "/tmp/unused", url="http://es.example:9200", **options)
        writer._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return writer

    def _ok(self, seen):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"errors": False, "items": []})

        return handler

    def test_it_sends_one_action_and_one_document_per_record(self, records) -> None:
        _compiled, rows = records
        seen: list = []
        writer = self._writer(self._ok(seen), index="people")

        asyncio.run(writer.write_batch(rows[:3]))
        asyncio.run(writer.close())

        lines = seen[0].content.decode().strip().split("\n")
        assert len(lines) == 6
        assert json.loads(lines[0])["index"]["_index"] == "people"
        assert json.loads(lines[1])["employee_id"].startswith("EMP-")
        assert writer.records_written == 3

    def test_the_document_id_is_the_records_position(self, records) -> None:
        """So a re-run overwrites rather than duplicating."""
        _compiled, rows = records
        seen: list = []
        writer = self._writer(self._ok(seen))
        asyncio.run(writer.write_batch(rows[:2]))

        again: list = []
        twice = self._writer(self._ok(again))
        asyncio.run(twice.write_batch(rows[:2]))

        first = [json.loads(line) for line in seen[0].content.decode().strip().split("\n")[::2]]
        second = [json.loads(line) for line in again[0].content.decode().strip().split("\n")[::2]]
        assert [item["index"]["_id"] for item in first] == [item["index"]["_id"] for item in second]

    def test_a_partial_failure_is_a_failure(self, records) -> None:
        """A 200 whose body says `errors` describes documents that are not there."""
        import httpx

        _compiled, rows = records

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "errors": True,
                    "items": [{"index": {"error": {"reason": "mapper_parsing_exception"}}}],
                },
            )

        writer = self._writer(handler)
        with pytest.raises(OutputError, match="mapper_parsing_exception"):
            asyncio.run(writer.write_batch(rows[:1]))

    def test_a_refused_batch_is_reported(self, records) -> None:
        import httpx

        _compiled, rows = records
        writer = self._writer(lambda request: httpx.Response(403, text="forbidden"))
        with pytest.raises(OutputError, match="403"):
            asyncio.run(writer.write_batch(rows[:1]))

    def test_it_batches_at_the_bulk_size(self, records) -> None:
        _compiled, rows = records
        seen: list = []
        writer = self._writer(self._ok(seen), bulk_size=5)
        asyncio.run(writer.write_batch(rows[:12]))
        assert len(seen) == 3

    def test_without_a_url_it_says_so(self) -> None:
        with pytest.raises(OutputError, match="url"):
            create_writer("elasticsearch", "/tmp/unused")


class TestObjectStorage:
    """Write the ordinary file, then put it in a bucket."""

    def _uploaded(self):
        seen: list[tuple[str, str, str, int]] = []

        def uploader(source: Path, bucket: str, key: str) -> None:
            seen.append((source.name, bucket, key, source.stat().st_size))

        return seen, uploader

    def test_the_local_file_is_a_normal_file(self, tmp_path: Path, records) -> None:
        _compiled, rows = records
        _seen, uploader = self._uploaded()

        write(
            "s3",
            tmp_path / "employee",
            rows[:10],
            columns=list(rows[0].values),
            bucket="datasets",
            format="jsonl",
            uploader=uploader,
        )

        written = tmp_path / "employee.jsonl"
        assert written.exists()
        assert len(written.read_text().strip().split("\n")) == 10

    def test_it_uploads_once_when_it_closes(self, tmp_path: Path, records) -> None:
        _compiled, rows = records
        seen, uploader = self._uploaded()

        write(
            "s3",
            tmp_path / "employee",
            rows[:10],
            columns=list(rows[0].values),
            bucket="datasets",
            prefix="2026/q1",
            uploader=uploader,
        )

        assert len(seen) == 1
        _name, bucket, key, size = seen[0]
        assert (bucket, key) == ("datasets", "2026/q1/employee.jsonl")
        assert size > 0

    def test_it_can_take_the_local_copy_away(self, tmp_path: Path, records) -> None:
        _compiled, rows = records
        _seen, uploader = self._uploaded()

        write(
            "s3",
            tmp_path / "employee",
            rows[:5],
            columns=list(rows[0].values),
            bucket="datasets",
            keep_local=False,
            uploader=uploader,
        )

        assert not (tmp_path / "employee.jsonl").exists()

    def test_any_file_format_can_go_in_a_bucket(self, tmp_path: Path, records) -> None:
        _compiled, rows = records
        seen, uploader = self._uploaded()

        write(
            "s3",
            tmp_path / "employee",
            rows[:5],
            columns=list(rows[0].values),
            bucket="datasets",
            format="csv",
            uploader=uploader,
        )

        assert seen[0][2].endswith(".csv")
        assert (tmp_path / "employee.csv").exists()

    def test_without_a_bucket_it_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(OutputError, match="bucket"):
            create_writer("s3", tmp_path / "employee")

    def test_an_unknown_inner_format_lists_the_ones_there_are(self, tmp_path: Path) -> None:
        with pytest.raises(OutputError, match="jsonl"):
            create_writer("s3", tmp_path / "employee", bucket="datasets", format="papyrus")


class TestTheRegistryDescribesWhatEachFormatNeeds:
    def test_a_destination_that_needs_configuring_says_which_option(self) -> None:
        from cacophony.outputs import describe_formats

        formats = {entry["name"]: entry for entry in describe_formats()}
        assert formats["elasticsearch"]["requires"] == ["url"]
        assert formats["s3"]["requires"] == ["bucket"]
        assert formats["jsonl"]["requires"] == []

    def test_opensearch_is_the_same_writer_under_another_name(self) -> None:
        assert OUTPUT_FORMATS["opensearch"] is OUTPUT_FORMATS["elasticsearch"]
