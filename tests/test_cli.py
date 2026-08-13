"""The command-line interface, end to end (design document sections 37 and 88).

Section 88 asks for integration tests covering
``schema -> generator -> validation -> export``. Driving the CLI is the
cheapest way to cover that whole path, because it is the same path a user
takes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cacophony.cli.main import app
from helpers import TEMPLATES

runner = CliRunner()

SIMPLE_YAML = """
project:
  name: CLI Test
  seed: 4242

entities:
  widget:
    count: 20
    primary_key: sku
    fields:
      sku:
        type: string
        generator: sequence
        format: "SKU-{0000}"
        unique: true
      colour:
        type: enum
        generator: weighted
        choices: [red, green, blue]
      price:
        type: decimal
        generator: random
        min: 1
        max: 100
        precision: 2
      label:
        type: string
        generator: template
        template: "{colour|title} widget {sku}"
"""


@pytest.fixture
def project_file(tmp_path: Path) -> Path:
    path = tmp_path / "project.yaml"
    path.write_text(SIMPLE_YAML, encoding="utf-8")
    return path


def invoke(*args: str):
    return runner.invoke(app, list(args))


class TestValidate:
    def test_valid_project_exits_zero(self, project_file: Path) -> None:
        result = invoke("validate", str(project_file))
        assert result.exit_code == 0
        assert "schema is valid" in result.stdout

    def test_invalid_project_exits_two(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("project:\n  name: x\nentities: {}\n", encoding="utf-8")
        assert invoke("validate", str(path)).exit_code == 2

    def test_missing_file_exits_two(self, tmp_path: Path) -> None:
        assert invoke("validate", str(tmp_path / "nope.yaml")).exit_code == 2

    def test_seed_can_be_overridden(self, project_file: Path) -> None:
        assert invoke("validate", str(project_file), "--seed", "7").exit_code == 0


class TestPlanAndLint:
    def test_plan_lists_every_field_and_its_generator(self, project_file: Path) -> None:
        result = invoke("plan", str(project_file))
        assert result.exit_code == 0
        for expected in ("widget", "sku", "sequence", "weighted", "template"):
            assert expected in result.stdout

    def test_plan_as_json(self, project_file: Path) -> None:
        result = invoke("plan", str(project_file), "--json")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["entity_order"] == ["widget"]
        assert payload["estimate"]["records"] == 20

    def test_lint_clean_project(self, project_file: Path) -> None:
        result = invoke("lint", str(project_file))
        assert result.exit_code == 0

    def test_lint_exits_one_on_errors(self, tmp_path: Path) -> None:
        path = tmp_path / "dupes.yaml"
        path.write_text(
            """
project:
  name: Dupes
  seed: 1
entities:
  e:
    count: 100
    fields:
      k:
        type: string
        unique: true
        generator: weighted
        choices: [a, b]
""",
            encoding="utf-8",
        )
        result = invoke("lint", str(path))
        assert result.exit_code == 1
        assert "unique-exhaustion" in result.stdout


class TestPreview:
    def test_default_preview(self, project_file: Path) -> None:
        result = invoke("preview", str(project_file), "-n", "5")
        assert result.exit_code == 0
        assert "widget" in result.stdout

    def test_json_preview_is_parseable(self, project_file: Path) -> None:
        result = invoke("preview", str(project_file), "-n", "3", "--json")
        assert result.exit_code == 0
        rows = [json.loads(line) for line in result.stdout.strip().splitlines()]
        assert len(rows) == 3
        assert rows[0]["sku"] == "SKU-0001"
        assert rows[0]["label"].endswith("SKU-0001")

    def test_preview_is_faithful_to_a_real_run(self, project_file: Path, tmp_path: Path) -> None:
        """What preview shows is what generate writes."""
        previewed = [
            json.loads(line)
            for line in invoke("preview", str(project_file), "-n", "5", "--json")
            .stdout.strip()
            .splitlines()
        ]
        out = tmp_path / "out"
        invoke("generate", str(project_file), "-o", "jsonl", "-d", str(out))
        written = [
            json.loads(line)
            for line in (out / "widget.jsonl").read_text(encoding="utf-8").strip().splitlines()
        ]
        assert previewed == written[:5]

    def test_isolate_gives_a_different_sample(self, project_file: Path) -> None:
        faithful = invoke("preview", str(project_file), "-n", "3", "--json").stdout
        isolated = invoke("preview", str(project_file), "-n", "3", "--json", "--isolate").stdout
        assert faithful != isolated

    def test_column_selection(self, project_file: Path) -> None:
        result = invoke("preview", str(project_file), "-n", "2", "-c", "sku,colour")
        assert result.exit_code == 0
        assert "price" not in result.stdout

    def test_offset(self, project_file: Path) -> None:
        result = invoke("preview", str(project_file), "-n", "1", "--offset", "9", "--json")
        assert json.loads(result.stdout.strip())["sku"] == "SKU-0010"


class TestGenerate:
    @pytest.mark.parametrize("fmt", ["jsonl", "json", "csv", "parquet"])
    def test_every_format_writes_a_file(self, project_file: Path, tmp_path: Path, fmt: str) -> None:
        out = tmp_path / fmt
        result = invoke("generate", str(project_file), "-o", fmt, "-d", str(out))
        assert result.exit_code == 0, result.stdout
        written = list(out.glob("widget.*"))
        assert len(written) == 1 and written[0].stat().st_size > 0
        assert "complete" in result.stdout

    def test_record_count_override(self, project_file: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        invoke("generate", str(project_file), "-n", "7", "-d", str(out))
        lines = (out / "widget.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 7

    def test_output_is_reproducible(self, project_file: Path, tmp_path: Path) -> None:
        first, second = tmp_path / "a", tmp_path / "b"
        invoke("generate", str(project_file), "-d", str(first))
        invoke("generate", str(project_file), "-d", str(second))
        assert (first / "widget.jsonl").read_bytes() == (second / "widget.jsonl").read_bytes()

    def test_seed_changes_the_output(self, project_file: Path, tmp_path: Path) -> None:
        first, second = tmp_path / "a", tmp_path / "b"
        invoke("generate", str(project_file), "-d", str(first), "--seed", "1")
        invoke("generate", str(project_file), "-d", str(second), "--seed", "2")
        assert (first / "widget.jsonl").read_bytes() != (second / "widget.jsonl").read_bytes()

    def test_unknown_format_exits_two(self, project_file: Path, tmp_path: Path) -> None:
        result = invoke("generate", str(project_file), "-o", "papyrus", "-d", str(tmp_path))
        assert result.exit_code == 2
        # Errors belong on stderr so `cacophony preview --json | jq` stays usable.
        assert "Available" in result.stderr

    def test_unknown_entity_exits_two(self, project_file: Path, tmp_path: Path) -> None:
        result = invoke("generate", str(project_file), "-e", "ghost", "-d", str(tmp_path))
        assert result.exit_code == 2

    def test_unknown_provenance_mode_exits_two(self, project_file: Path, tmp_path: Path) -> None:
        result = invoke(
            "generate", str(project_file), "--provenance", "telepathy", "-d", str(tmp_path)
        )
        assert result.exit_code == 2

    def test_provenance_reaches_the_file(self, project_file: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        invoke("generate", str(project_file), "-d", str(out), "--provenance", "field")
        first = json.loads((out / "widget.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert first["_provenance"]["fields"]["sku"]["generator"] == "sequence"

    def test_single_entity_selection(self, project_file: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        invoke("generate", str(project_file), "-e", "widget", "-d", str(out))
        assert (out / "widget.jsonl").exists()

    def test_generation_failure_exits_three(self, tmp_path: Path) -> None:
        path = tmp_path / "boom.yaml"
        path.write_text(
            """
project:
  name: Boom
  seed: 1
entities:
  e:
    count: 3
    fields:
      words:
        type: text
        semantic: Some prose
        generator: llm
""",
            encoding="utf-8",
        )
        result = invoke("generate", str(path), "-d", str(tmp_path / "out"))
        assert result.exit_code == 3
        assert "provider phase" in result.stderr


class TestStreamSeparation:
    def test_errors_go_to_stderr_not_stdout(self, tmp_path: Path) -> None:
        """`cacophony preview --json | jq` must never receive an error message."""
        result = invoke("validate", str(tmp_path / "missing.yaml"))
        assert result.exit_code == 2
        assert result.stdout == ""
        assert "error" in result.stderr

    def test_json_preview_stdout_is_pure_json(self, project_file: Path) -> None:
        result = invoke("preview", str(project_file), "-n", "2", "--json")
        for line in result.stdout.strip().splitlines():
            json.loads(line)


class TestInformationalCommands:
    def test_generators_lists_the_registry(self) -> None:
        result = invoke("generators")
        assert result.exit_code == 0
        for name in ("sequence", "weighted", "faker", "expression"):
            assert name in result.stdout

    def test_generators_as_json(self) -> None:
        rows = json.loads(invoke("generators", "--json").stdout)
        assert any(row["name"] == "sequence" for row in rows)
        assert all("summary" in row for row in rows)

    def test_providers_without_a_project(self) -> None:
        assert invoke("providers").exit_code == 0

    def test_providers_with_a_project(self) -> None:
        result = invoke("providers", str(TEMPLATES / "helpdesk.yaml"))
        assert result.exit_code == 0
        assert "local_llm" in result.stdout

    def test_version(self) -> None:
        from cacophony import __version__

        result = invoke("version")
        assert result.exit_code == 0 and __version__ in result.stdout

    def test_no_arguments_shows_help(self) -> None:
        result = invoke()
        assert "validate" in result.stdout and "generate" in result.stdout


class TestShippedTemplatesEndToEnd:
    def test_corporate_directory_generates(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = invoke(
            "generate",
            str(TEMPLATES / "corporate-directory.yaml"),
            "-n",
            "50",
            "-d",
            str(out),
            "-o",
            "jsonl",
        )
        assert result.exit_code == 0, result.stdout
        for entity in ("employee", "device", "location"):
            lines = (out / f"{entity}.jsonl").read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 50

    def test_generated_emails_use_reserved_domains(self, tmp_path: Path) -> None:
        """Section 62, verified on real output rather than on the schema."""
        out = tmp_path / "out"
        invoke(
            "generate",
            str(TEMPLATES / "corporate-directory.yaml"),
            "-n",
            "100",
            "-e",
            "employee",
            "-d",
            str(out),
        )
        for line in (out / "employee.jsonl").read_text(encoding="utf-8").splitlines():
            domain = json.loads(line)["email"].split("@")[1]
            assert domain in {"example.com", "example.org", "example.net"} or domain.endswith(
                (".example", ".test", ".invalid")
            )

    def test_helpdesk_runs_with_placeholders(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = invoke("generate", str(TEMPLATES / "helpdesk.yaml"), "-n", "20", "-d", str(out))
        assert result.exit_code == 0, result.stdout
        first = json.loads((out / "ticket.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert "PLACEHOLDER" in first["summary"]

    def test_security_operations_generates(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = invoke(
            "generate", str(TEMPLATES / "security-operations.yaml"), "-n", "25", "-d", str(out)
        )
        assert result.exit_code == 0, result.stdout
        row = json.loads((out / "authentication.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert row["source_ip"].startswith(("192.0.2.", "198.51.100.", "203.0.113."))
